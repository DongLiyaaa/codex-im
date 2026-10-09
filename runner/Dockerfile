# amd64 by default. The Codex sandbox cannot install its seccomp filter in an amd64 container that Docker Desktop
# translates with Rosetta on Apple Silicon; compose.runner-arm64.yaml is the explicit opt-in for a native build.
ARG RUNNER_PLATFORM=linux/amd64
FROM --platform=${RUNNER_PLATFORM} node:22.16.0-bookworm-slim AS node
FROM --platform=${RUNNER_PLATFORM} python:3.12.10-slim-bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && npm install --global --registry=https://registry.npmjs.org @openai/codex@0.157.1 @anthropic-ai/claude-code@2.1.286 \
    && npm cache clean --force \
    && codex --version \
    && claude --version \
    && useradd --uid 10001 --create-home runner
WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py sandbox_probe.py ./
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
ARG VERSION=0.0.3
LABEL org.opencontainers.image.source="https://github.com/DongLiyaaa/codex-im" \
      org.opencontainers.image.description="Agent Hub 执行服务（Runner，Codex CLI 与可选 Claude CLI）" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="NOASSERTION"
EXPOSE 8081
# /health runs the sandbox preflight (cached for a minute), so it needs more than a few seconds when cold.
HEALTHCHECK --interval=30s --timeout=15s --start-period=30s CMD python -c "import json,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:8081/health', timeout=14)); raise SystemExit(d['status'] != 'ready')"
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8081", "--workers", "1", "--no-access-log"]
